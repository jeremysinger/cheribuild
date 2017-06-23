import os
import pprint
from enum import Enum
from pathlib import Path


from ...config.loader import ComputedDefaultValue
from ...config.chericonfig import CrossCompileTarget
from ..cheribsd import BuildCHERIBSD
from ..llvm import BuildLLVM
from ...project import *
from ...utils import *

__all__ = ["CheriConfig", "CrossCompileCMakeProject", "CrossCompileAutotoolsProject", "CrossCompileTarget",
           "CrossCompileProject", "CrossInstallDir"]

class CrossInstallDir(Enum):
    NONE = 0
    CHERIBSD_ROOTFS = 1
    SDK = 2

defaultTarget = ComputedDefaultValue(
    function=lambda config, project: config.crossCompileTarget.value,
    asString="'cheri' unless -xmips/-xhost is set")

def _default_build_dir(config: CheriConfig, project):
    return project.buildDirForTarget(config, project.crossCompileTarget)

def _installDir(config: CheriConfig, project: "CrossCompileProject"):
    if project.crossCompileTarget == CrossCompileTarget.NATIVE:
        return config.sdkDir
    if project.crossInstallDir == CrossInstallDir.CHERIBSD_ROOTFS:
        return Path(BuildCHERIBSD.rootfsDir(config) / "extra" / project.projectName.lower())
    elif project.crossInstallDir == CrossInstallDir.SDK:
        return config.sdkSysrootDir
    fatalError("Unknown install dir for", project.projectName)

def _installDirMessage(project: "CrossCompileProject"):
    if project.crossInstallDir == CrossInstallDir.CHERIBSD_ROOTFS:
        return "$CHERIBSD_ROOTFS/extra/" + project.projectName.lower() + " or $CHERI_SDK for --xhost build"
    elif project.crossInstallDir == CrossInstallDir.SDK:
        return "$CHERI_SDK/sysroot for cross builds or $CHERI_SDK for --xhost build"
    return "UNKNOWN"


class CrossCompileProject(Project):
    doNotAddToTargets = True
    crossInstallDir = CrossInstallDir.CHERIBSD_ROOTFS
    defaultInstallDir = ComputedDefaultValue(function=_installDir, asString=_installDirMessage)
    appendCheriBitsToBuildDir = True
    dependencies = ["cheribsd-sdk"]
    defaultLinker = "lld"
    crossCompileTarget = None  # type: CrossCompileTarget
    defaultOptimizationLevel = ["-O2"]
    warningFlags = ["-Wall", "-Werror=cheri-capability-misuse", "-Werror=implicit-function-declaration",
                    "-Werror=format", "-Werror=undefined-internal", "-Werror=incompatible-pointer-types",
                    "-Werror=mips-cheri-prototypes"]
    defaultBuildDir = ComputedDefaultValue(
        function=_default_build_dir,
        asString=lambda cls: "$BUILD_ROOT/" + cls.projectName.lower()  + "-$CROSS_TARGET-build")

    def __init__(self, config: CheriConfig):
        super().__init__(config)
        self.compiler_dir = self.config.sdkBinDir
        # Use the compiler from the build directory for native builds to get stddef.h (which will be deleted)
        if self.crossCompileTarget == CrossCompileTarget.NATIVE:
            if (BuildLLVM.buildDir / "bin/clang").exists():
                self.compiler_dir = BuildLLVM.buildDir / "bin"

        self.targetTriple = None
        self.sdkBinDir = self.config.sdkDir / "bin"
        self.sdkSysroot = self.config.sdkDir / "sysroot"
        # compiler flags:
        if self.crossCompileTarget == CrossCompileTarget.NATIVE:
            self.COMMON_FLAGS = []
            self.targetTriple = self.get_host_triple()
            if self.crossInstallDir == CrossInstallDir.SDK:
                self.installDir = self.config.sdkDir
            elif self.crossInstallDir == CrossInstallDir.CHERIBSD_ROOTFS:
                self.installDir = self.buildDir / "test-install-prefix"
            else:
                assert self.installDir, "must be set"
        else:
            if self.crossInstallDir == CrossInstallDir.SDK:
                self.installPrefix = "/usr/local"
                self.destdir = config.sdkSysrootDir
            elif self.crossInstallDir == CrossInstallDir.CHERIBSD_ROOTFS:
                self.installPrefix = Path("/", self.installDir.relative_to(BuildCHERIBSD.rootfsDir(config)))
                self.destdir = BuildCHERIBSD.rootfsDir(config)
            else:
                assert self.installPrefix and self.destdir, "Must be set!"
            self.COMMON_FLAGS = ["-integrated-as", "-pipe", "-msoft-float", "-G0"]
            # use *-*-freebsd12 to default to libc++
            if self.crossCompileTarget == CrossCompileTarget.CHERI:
                self.targetTriple = "cheri-unknown-freebsd"
                self.COMMON_FLAGS.append("-mabi=purecap")
                if self.config.cheriBits == 128:
                    self.COMMON_FLAGS.append("-mcpu=cheri128")
            else:
                assert self.crossCompileTarget == CrossCompileTarget.MIPS
                self.targetTriple = "mips64-unknown-freebsd"
                self.COMMON_FLAGS.append("-mabi=n64")
            if not self.noUseMxgot:
                self.COMMON_FLAGS.append("-mxgot")
        if self.debugInfo:
            self.COMMON_FLAGS.append("-g")
        self.CFLAGS = []
        self.CXXFLAGS = []
        self.ASMFLAGS = []
        self.LDFLAGS = []

    @property
    def targetTripleWithVersion(self):
        # we need to append the FreeBSD version to pick up the correct C++ standard library
        if self.compiling_for_host():
            return self.targetTriple
        else:
            # anything over 10 should use libc++ by default
            return self.targetTriple + "12"

    @staticmethod
    def get_host_triple():
        # TODO: get --build from `clang --version | grep Target:`
        if IS_FREEBSD:
            buildhost = "x86_64-unknown-freebsd"
            # noinspection PyUnresolvedReferences
            release = os.uname().release
            buildhost += release[:release.index(".")]
        else:
            buildhost = "x86_64-unknown-linux-gnu"
        return buildhost

    @property
    def sizeof_void_ptr(self):
        if self.crossCompileTarget in (CrossCompileTarget.MIPS, CrossCompileTarget.NATIVE):
            return 8
        elif self.config.cheriBits == 128:
            return 16
        else:
            assert self.config.cheriBits == 256
            return 32

    @property
    def default_ldflags(self):
        if self.crossCompileTarget == CrossCompileTarget.NATIVE:
            # return ["-fuse-ld=" + self.linker]
            return []
        elif self.crossCompileTarget == CrossCompileTarget.CHERI:
            emulation = "elf64btsmip_cheri_fbsd"
            abi = "purecap"
        elif self.crossCompileTarget == CrossCompileTarget.MIPS:
            emulation = "elf64btsmip_fbsd"
            abi = "n64"
        else:
            fatalError("Logic error!")
            return []
        result = ["-mabi=" + abi,
                  "-Wl,-m" + emulation,
                  "-fuse-ld=" + self.linker,
                  "-Wl,-z,notext",  # needed so that LLD allows text relocations
                  "--sysroot=" + str(self.sdkSysroot),
                  "-B" + str(self.sdkBinDir)]
        if self.compiling_for_cheri() and self.newCapRelocs:
            # TODO: check that we are using LLD and not BFD
            result += ["-no-capsizefix", "-Wl,-process-cap-relocs", "-Wl,-verbose"]
        if self.config.withLibstatcounters:
            #if self.linkDynamic:
            #    result.append("-lstatcounters")
            #else:
            result += ["-Wl,--whole-archive", "-lstatcounters", "-Wl,--no-whole-archive"]
        return result

    @classmethod
    def setupConfigOptions(cls, **kwargs):
        super().setupConfigOptions(**kwargs)
        cls.noUseMxgot = cls.addBoolOption("no-use-mxgot", help="Compile without -mxgot flag (Unless the program is"
                                                                " small this will probably break everything!)")
        cls.linker = cls.addConfigOption("linker", default=cls.defaultLinker,
                                         help="The linker to use (`lld` or `bfd`) (lld is  better but may"
                                              " not work for some projects!)")
        cls.debugInfo = cls.addBoolOption("debug-info", help="build with debug info", default=True)
        cls.optimizationFlags = cls.addConfigOption("optimization-flags", kind=list, metavar="OPTIONS",
                                                    default=cls.defaultOptimizationLevel)
        # TODO: check if LLD supports it and if yes default to true?
        cls.newCapRelocs = cls.addBoolOption("new-cap-relocs", help="Use the new __cap_relocs processing in LLD", default=False)
        if cls.crossCompileTarget is None:
            cls.crossCompileTarget = cls.addConfigOption("target", help="The target to build for (`cheri` or `mips` or `native`)",
                                                 default=defaultTarget, choices=["cheri", "mips", "native"],
                                                 kind=CrossCompileTarget)

    @classmethod
    def buildDirForTarget(cls, config: CheriConfig, target: CrossCompileTarget):
        if target == CrossCompileTarget.CHERI:
            build_dir_suffix = config.cheriBitsStr + "-build"
        else:
            build_dir_suffix = target.value + "-build"
        return config.buildRoot / (cls.projectName.lower() + "-" + build_dir_suffix)

    def compiling_for_mips(self):
        return self.crossCompileTarget == CrossCompileTarget.MIPS

    def compiling_for_cheri(self):
        return self.crossCompileTarget == CrossCompileTarget.CHERI

    def compiling_for_host(self):
        return self.crossCompileTarget == CrossCompileTarget.NATIVE

class CrossCompileCMakeProject(CMakeProject, CrossCompileProject):
    doNotAddToTargets = True  # only used as base class
    defaultCMakeBuildType = "RelWithDebInfo"  # default to O2

    @classmethod
    def setupConfigOptions(cls, **kwargs):
        super().setupConfigOptions(**kwargs)

    def __init__(self, config: CheriConfig):
        super().__init__(config)
        # This must come first:
        if self.crossCompileTarget == CrossCompileTarget.NATIVE:
            self._cmakeTemplate = includeLocalFile("files/NativeToolchain.cmake.in")
            self.toolchainFile = self.buildDir / "NativeToolchain.cmake"
        else:
            self._cmakeTemplate = includeLocalFile("files/CheriBSDToolchain.cmake.in")
            self.toolchainFile = self.buildDir / "CheriBSDToolchain.cmake"
        self.add_cmake_options(CMAKE_TOOLCHAIN_FILE=self.toolchainFile)
        # The toolchain files need at least CMake 3.6
        self.set_minimum_cmake_version(3, 6)

    def _prepareToolchainFile(self, **kwargs):
        configuredTemplate = self._cmakeTemplate
        for key, value in kwargs.items():
            if value is None:
                continue
            strval = " ".join(value) if isinstance(value, list) else str(value)
            assert "@" + key + "@" in configuredTemplate, key
            configuredTemplate = configuredTemplate.replace("@" + key + "@", strval)
        assert "@" not in configuredTemplate, configuredTemplate
        self.writeFile(contents=configuredTemplate, file=self.toolchainFile, overwrite=True, noCommandPrint=True)

    def configure(self, **kwargs):
        self.COMMON_FLAGS.append("-B" + str(self.sdkBinDir))

        if self.compiling_for_host():
            common_flags = self.COMMON_FLAGS
        else:
            if self._get_cmake_version() < (3, 9, 0) and not (self.sdkSysroot / "usr/local/lib/cheri").exists():
                warningMessage("Workaround for missing custom lib suffix in CMake < 3.9")
                # create a /usr/lib/cheri -> /usr/libcheri symlink so that cmake can find the right libraries
                self.createSymlink(Path("../libcheri"), self.sdkSysroot / "usr/lib/cheri", relative=True,
                                   cwd=self.sdkSysroot / "usr/lib")
                self.makedirs(self.sdkSysroot / "usr/local/lib")
                self.makedirs(self.sdkSysroot / "usr/local/libcheri")
                self.createSymlink(Path("../libcheri"), self.sdkSysroot / "usr/local/lib/cheri",
                                   relative=True, cwd=self.sdkSysroot / "usr/local/lib")
            common_flags = self.COMMON_FLAGS + self.warningFlags + ["-target", self.targetTripleWithVersion]

        clang = self.config.clangPath if self.compiling_for_host() else self.compiler_dir / "clang"
        clangxx = self.config.clangPlusPlusPath if self.compiling_for_host() else self.compiler_dir / "clang++"
        if self.compiling_for_cheri():
            add_lib_suffix = """
# cheri libraries are found in /usr/libcheri:
if("${CMAKE_VERSION}" VERSION_LESS 3.9)
  # message(STATUS "CMAKE < 3.9 HACK to find libcheri libraries")
  # need to create a <sysroot>/usr/lib/cheri -> <sysroot>/usr/libcheri symlink 
  set(CMAKE_LIBRARY_ARCHITECTURE "cheri")
  set(CMAKE_SYSTEM_LIBRARY_PATH "${CMAKE_FIND_ROOT_PATH}/usr/libcheri;${CMAKE_FIND_ROOT_PATH}/usr/local/libcheri")
else()
    set(CMAKE_FIND_LIBRARY_CUSTOM_LIB_SUFFIX "cheri")
endif()
set(LIB_SUFFIX "cheri" CACHE INTERNAL "")
"""
            processor = "CHERI (MIPS IV compatible)"
        elif self.compiling_for_mips():
            add_lib_suffix = "# no lib suffix for mips libraries"
            processor = "BERI (MIPS IV compatible)"
        else:
            add_lib_suffix = None
            processor = None
        self._prepareToolchainFile(
            TOOLCHAIN_SDK_BINDIR=self.sdkBinDir,
            TOOLCHAIN_COMPILER_BINDIR=self.compiler_dir,
            TOOLCHAIN_TARGET_TRIPLE=self.targetTriple,
            TOOLCHAIN_COMMON_FLAGS=common_flags,
            TOOLCHAIN_C_FLAGS=self.CFLAGS,
            TOOLCHAIN_LINKER_FLAGS=self.LDFLAGS + self.default_ldflags,
            TOOLCHAIN_CXX_FLAGS=self.CXXFLAGS,
            TOOLCHAIN_ASM_FLAGS=self.ASMFLAGS,
            TOOLCHAIN_C_COMPILER=clang,
            TOOLCHAIN_CXX_COMPILER=clangxx,
            TOOLCHAIN_SYSROOT=self.sdkSysroot if not self.compiling_for_host() else None,
            ADD_TOOLCHAIN_LIB_SUFFIX=add_lib_suffix,
            TOOLCHAIN_SYSTEM_PROCESSOR=processor,
        )
        # TODO: BUILD_SHARED_LIBS=OFF?
        super().configure()


class CrossCompileAutotoolsProject(AutotoolsProject, CrossCompileProject):
    doNotAddToTargets = True  # only used as base class

    add_host_target_build_config_options = True
    _configure_supports_libdir = True  # override in nginx
    _configure_supports_variables_on_cmdline = True  # override in nginx

    def __init__(self, config: CheriConfig):
        super().__init__(config)
        buildhost = self.get_host_triple()
        if not self.compiling_for_host() and self.add_host_target_build_config_options:
            self.configureArgs.extend(["--host=" + self.targetTriple, "--target=" + self.targetTriple,
                                       "--build=" + buildhost])

    @property
    def default_compiler_flags(self):
        result = self.COMMON_FLAGS + self.optimizationFlags + ["-target", self.targetTripleWithVersion]
        if self.crossCompileTarget != CrossCompileTarget.NATIVE:
            result += ["--sysroot=" + str(self.sdkSysroot), "-B" + str(self.sdkBinDir)] + self.warningFlags
        return result

    def add_configure_env_arg(self, arg: str, value: str):
        if not value:
            return
        self.configureEnvironment[arg] = value
        if self._configure_supports_variables_on_cmdline:
            self.configureArgs.append(arg + "=" + value)

    def set_prog_with_args(self, prog: str, path: Path, args: list):
        fullpath = str(path)
        if args:
            fullpath += " " + " ".join(args)
        self.configureEnvironment[prog] = fullpath
        if self._configure_supports_variables_on_cmdline:
            self.configureArgs.append(prog + "=" + fullpath)

    def configure(self, **kwargs):
        CPPFLAGS = self.default_compiler_flags
        for key in ("CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS"):
            assert key not in self.configureEnvironment
        # target triple contains a number suffix -> remove it when computing the compiler name
        compiler_prefix = self.targetTriple + "-"
        if self.crossCompileTarget == CrossCompileTarget.NATIVE:
            compiler_prefix = ""
        elif self.compiling_for_cheri() and self._configure_supports_libdir:
            # nginx configure script doesn't understand --libdir
            # make sure that we install to the right directory
            # TODO: can we use relative paths?
            self.configureArgs.append("--libdir=" + str(self.installPrefix) + "/libcheri")

        cc = self.config.clangPath if self.compiling_for_host() else self.compiler_dir / (compiler_prefix + "clang")
        cxx = self.config.clangPlusPlusPath if self.compiling_for_host() else self.compiler_dir / (compiler_prefix + "clang++")
        # autotools overrides CFLAGS -> use CC and CXX vars here
        self.set_prog_with_args("CC", cc, CPPFLAGS + self.CFLAGS)
        self.set_prog_with_args("CXX", cxx, CPPFLAGS + self.CXXFLAGS)
        # self.add_configure_env_arg("CPPFLAGS", " ".join(CPPFLAGS))
        # self.add_configure_env_arg("CFLAGS", " ".join(CPPFLAGS + self.CFLAGS))
        # self.add_configure_env_arg("CXXFLAGS", " ".join(CPPFLAGS + self.CXXFLAGS))
        # this one seems to work:
        self.add_configure_env_arg("LDFLAGS", " ".join(self.LDFLAGS + self.default_ldflags))


        if not self.compiling_for_host():
            self.set_prog_with_args("CPP", self.compiler_dir / (compiler_prefix + "clang-cpp"), CPPFLAGS)
            if "lld" in self.linker and (self.compiler_dir / "ld.lld").exists():
                self.add_configure_env_arg("LD", str(self.compiler_dir / "ld.lld"))

        # remove all empty items from environment:
        env = {k: v for k, v in self.configureEnvironment.items() if v}
        self.configureEnvironment.clear()
        self.configureEnvironment.update(env)
        print(coloured(AnsiColour.yellow, "Cross configure environment:",
                       pprint.pformat(self.configureEnvironment, width=160)))
        super().configure(**kwargs)

    def process(self):
        # We run all these commands with $PATH containing $CHERI_SDK/bin to ensure the right tools are used
        with setEnv(PATH=str(self.config.sdkDir / "bin") + ":" + os.getenv("PATH")):
            super().process()
