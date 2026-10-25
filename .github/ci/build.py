"""Cold builds on disposable runners. Private inputs and compiler logs stay local."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

HERE = Path(__file__).resolve().parent
XENON = 'ddd128bcca99fe8bfbb99bea583c972351fa6ace'
SDL = '829a65d769d935c4852f8159e964312c0957260a'
FFMPEG = 'b08d7969c550a804a59511c7b83f2dd8cc0499b8'
VKD3D = 'f732fb78899aea8e3d4107b72b23f7f54500ffb7'


class Build:
    def __init__(self, args):
        self.target = args.target
        self.source = args.source.resolve()
        self.inputs = args.inputs.resolve()
        self.output = args.output.resolve()
        self.work = Path(os.environ.get('RUNNER_TEMP', self.source.parent / 'ci-temp')) / 'ttt2-build'
        self.work.mkdir(parents=True, exist_ok=True)
        self.env = os.environ.copy()
        self.env.update(TTT2_BUILD_JOBS='2', XDG_CACHE_HOME=str(self.work / 'cache'))
        self.counter = 0
        self.jobs = '2'
        self.cc, self.cxx = ('clang-cl', 'clang-cl') if args.target == 'windows' else ('clang', 'clang++')
        self.env.update(CC=self.cc, CXX=self.cxx)

    def run(self, label, *args, cwd=None, env=None, public_output=False):
        print(label, flush=True)
        self.counter += 1
        # Compiler errors and recompiler output can quote original game bytes.
        # Never echo these logs or upload them from the public workflow.
        with (self.work / f'private-log-{self.counter}.txt').open('wb') as log:
            command = [a.as_posix() if isinstance(a, Path) else str(a) for a in args]
            if command[0] == 'bash':
                command[0] = self.env.get('CI_BASH', 'bash')
            result = subprocess.run(command, cwd=cwd or self.source,
                                    env=env or self.env, stdout=log, stderr=subprocess.STDOUT)
        if result.returncode:
            if public_output:
                print('\n'.join((self.work / f'private-log-{self.counter}.txt').read_text(errors='replace').splitlines()[-60:]), flush=True)
            raise RuntimeError(f'{label} failed (exit {result.returncode}); private log withheld')

    def clone(self, url, revision, destination):
        self.run('Fetch pinned dependency', 'git', 'init', destination)
        self.run('Fetch pinned revision', 'git', '-C', destination, 'fetch', '--depth=1', url, revision)
        self.run('Check out dependency', 'git', '-C', destination, 'checkout', '--detach', 'FETCH_HEAD')

    def cmake(self, source, build, *options):
        # CMake embeds these values in generated source lists; Windows
        # backslashes would become escapes such as \a instead of separators.
        options = tuple(option.replace('\\', '/') for option in options)
        self.run('Configure ' + source.name, 'cmake', '-S', source, '-B', build, '-G', 'Ninja',
                 '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_POLICY_VERSION_MINIMUM=3.5',
                 '-DCMAKE_C_COMPILER=' + self.cc, '-DCMAKE_CXX_COMPILER=' + self.cxx, *options,
                 public_output=True)

    def compile(self, build, *targets):
        self.run('Compile ' + build.name, 'cmake', '--build', build, '--parallel', self.jobs,
                 *(['--target', *targets] if targets else []), public_output=build.name != 'game-build')

    def script(self, relative, *args):
        public_scripts = {
            'port/tools/build_android_sdl_shell.sh': 'android-sdl-shell',
            'port/tools/build_android_ffmpeg_xma.sh': 'android-ffmpeg-xma',
            'port/tools/build_android_vkd3d_shader.sh': 'android-vkd3d-1.17',
            'port/tools/build_android_openssl.sh': 'android-openssl',
        }
        try:
            self.run(Path(relative).name, sys.executable if relative.endswith('.py') else 'bash',
                     self.source / relative, *args, public_output=relative in public_scripts)
        except RuntimeError:
            # Only open-source dependency logs are eligible for public diagnostics.
            # Generated game code, signing logs and the combined APK build stay private.
            if relative in public_scripts:
                directory = self.work / 'cache/ttt2-platform' / public_scripts[relative]
                for log in sorted(directory.glob('*.log')):
                    print(log.name + ':\n' + '\n'.join(log.read_text(errors='replace').splitlines()[-30:]), flush=True)
            raise

    def generate(self):
        manifest = json.loads((self.inputs / 'manifest.json').read_text())
        for name in ('Default.xex', 'Default.xexp'):
            data = (self.inputs / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != manifest['sha256'][name]:
                raise RuntimeError('Private input integrity check failed')
        xenon = self.source / 'tools/XenonRecomp'
        self.clone('https://github.com/hedge-dev/XenonRecomp.git', XENON, xenon)
        self.run('Fetch recompiler dependencies', 'git', '-C', xenon, 'submodule', 'update', '--init', '--recursive', '--depth=1')
        self.run('Apply reviewed recompiler fixes', 'git', '-C', xenon, 'apply', self.source / 'tools/patches/XenonRecomp.patch')
        bootstrap = self.work / 'recompiler'
        self.cmake(HERE / 'xex', bootstrap, '-DXENON_SOURCE=' + str(xenon))
        self.compile(bootstrap, 'ci_xex', 'XenonRecomp')
        suffix = '.exe' if self.target == 'windows' else ''
        patched = self.source / 're/recomp/Default_patched_tu3.xex'
        image = self.source / 're/decrypted/Default_patched_tu3.bin'
        image.parent.mkdir(parents=True, exist_ok=True)
        self.run('Apply title update and decode image', bootstrap / ('ci_xex' + suffix),
                 self.inputs / 'Default.xex', self.inputs / 'Default.xexp', patched, image)
        # Use the checked-in jump tables, including the game's known corrections.
        self.script('port/tools/gen_missing_switches.py')
        ppc = self.source / 're/ppc'
        ppc.mkdir(parents=True, exist_ok=True)
        recompiler = bootstrap / 'xenon/XenonRecomp' / ('XenonRecomp' + suffix)
        for attempt in range(8):
            self.run('Generate portable PPC C++', recompiler, self.source / 're/recomp/tekken.toml', xenon / 'XenonUtils/ppc_context.h')
            units = list(ppc.glob('ppc_recomp.*.cpp'))
            if not units:
                raise RuntimeError('Recompiler produced no sources')
            if not any(b'// ERROR ' in p.read_bytes() for p in units):
                break
            self.script('port/tools/gen_missing_functions.py')
        else:
            raise RuntimeError('Generated function boundaries did not converge')
        self.script('port/tools/debug_probes.py')
        self.script('port/tools/check_recomp_acceptance.py')

    def dependencies(self):
        self.sdl = self.work / 'SDL'
        self.ffmpeg = self.source / 'tools/ffmpeg_dl/FFmpeg-n7.1'
        self.clone('https://github.com/libsdl-org/SDL.git', SDL, self.sdl)
        self.clone('https://github.com/FFmpeg/FFmpeg.git', FFMPEG, self.ffmpeg)
        self.prefix = self.work / 'install'
        if self.target != 'android':
            sdl_build = self.work / 'sdl-build'
            self.cmake(self.sdl, sdl_build, '-DSDL_SHARED=ON', '-DSDL_STATIC=OFF',
                       '-DSDL_TESTS=OFF', '-DCMAKE_INSTALL_LIBDIR=lib', '-DCMAKE_INSTALL_PREFIX=' + str(self.prefix))
            self.compile(sdl_build)
            self.run('Install SDL', 'cmake', '--install', sdl_build)
            self.env['CMAKE_PREFIX_PATH'] = str(self.prefix)
            self.env['PKG_CONFIG_PATH'] = str(self.prefix / 'lib/pkgconfig')

    def steam(self):
        if self.env.get('CI_STEAM') != 'true' or self.target == 'android':
            return None
        sdk = self.inputs / 'steamworks/sdk'
        if not (sdk / 'public/steam/steam_api.h').is_file():
            raise RuntimeError('Steam build requested but private Steamworks SDK is missing')
        return sdk

    def android(self):
        cache = self.work / 'cache/ttt2-platform'
        ndk = Path(self.env['ANDROID_HOME']) / 'ndk/30.0.16248370'
        self.env.update(TTT2_ANDROID_NDK=str(ndk), ANDROID_NDK_HOME=str(ndk),
                        TTT2_SDL_SOURCE=str(self.sdl), TTT2_GAME_SOURCE=str(self.source),
                        TTT2_FFMPEG_SOURCE=str(self.ffmpeg), TTT2_ANDROID_VARIANT='release',
                        OPENSSL_JOBS=self.jobs, TTT2_ANDROID_SIGNING_DIR=str(Path(self.env['RUNNER_TEMP']) / 'android-signing'))
        signing = Path(self.env['TTT2_ANDROID_SIGNING_DIR'])
        if not all((signing / name).is_file() for name in ('release.jks', 'password')):
            raise RuntimeError('Release signing secrets are required; refusing to generate a replacement key')
        self.script('port/tools/build_android_sdl_shell.sh')
        self.script('port/tools/build_android_vkd3d_shader.sh')
        self.script('port/tools/build_android_ffmpeg_xma.sh')
        self.script('port/tools/build_android_openssl.sh')
        self.script('port/tools/build_android_ppc_archive.py', '--ppc-dir', self.source / 're/ppc',
                    '--ndk', ndk, '--build-dir', cache / 'android-ppc-arm64-o2', '--opt-level', 'O2', '--jobs', self.jobs)
        # The APK script builds the pinned DXVK stack before linking the game.
        self.script('port/tools/build_android_game_apk.sh')
        shutil.copy2(self.source / 'dist/android/ttt2-android-arm64-release.apk', self.output)

    def configure_ffmpeg(self):
        build = self.work / 'ffmpeg-build'
        build.mkdir()
        options = ['--disable-everything', '--disable-programs', '--disable-doc', '--disable-avdevice',
                   '--disable-avformat', '--disable-swresample', '--disable-swscale', '--disable-postproc',
                   '--disable-avfilter', '--disable-network', '--disable-asm', '--disable-debug',
                   '--disable-autodetect', '--enable-decoder=wmapro,xma1,xma2', '--enable-parser=xma']
        if self.target == 'windows':
            options += ['--toolchain=msvc', '--target-os=win64', '--arch=x86_64', '--cc=clang-cl', '--cxx=clang-cl']
        else:
            options += ['--target-os=linux', '--arch=x86_64', '--cc=clang', '--cxx=clang++', '--enable-pic']
        self.run('Configure XMA decoder', 'bash', self.ffmpeg / 'configure', *options, cwd=build)
        return build

    def linux(self):
        ffbuild = self.configure_ffmpeg()
        vkd3d = self.work / 'vkd3d-source'
        vkbuild = self.work / 'vkd3d-build'
        vkbuild.mkdir()
        self.clone('https://gitlab.winehq.org/wine/vkd3d.git', VKD3D, vkd3d)
        self.run('Configure shader compiler sources', 'autoreconf', '-ifv', cwd=vkd3d)
        self.run('Configure shader compiler', vkd3d / 'configure', '--prefix=' + str(self.prefix),
                 '--disable-tests', '--disable-demos', '--disable-maintainer-mode',
                 '--with-xcb=no', '--with-opengl=no', '--with-ncurses=no', cwd=vkbuild)
        headers = ['include/' + p.stem + '.h' for p in (vkd3d / 'include').glob('*.idl') if p.name != 'vkd3d_unknown.idl']
        self.run('Generate shader headers', 'make', '-j', self.jobs, *headers,
                 'include/private/spirv_grammar.h', 'include/private/vkd3d_version.h', cwd=vkbuild)
        self.run('Compile shader compiler', 'make', '-j', self.jobs, 'libvkd3d-shader.la', cwd=vkbuild)
        # Install only the shader library; the full vkd3d D3D12 library is unused.
        (self.prefix / 'include/vkd3d').mkdir(parents=True, exist_ok=True)
        for name in ('vkd3d_shader.h', 'vkd3d_types.h'):
            shutil.copy2(vkd3d / 'include' / name, self.prefix / 'include/vkd3d')
        for lib in (vkbuild / '.libs').glob('libvkd3d-shader.so*'):
            shutil.copy2(lib, self.prefix / 'lib' / lib.name)
        self.script('tools/prepare_dxvk_native.py')
        dxvk = next(p for p in (self.work / 'cache/ttt2-platform/shared-dxvk').glob('source-*') if p.is_dir())
        dxbuild = self.work / 'dxvk-build'
        self.run('Configure DXVK', 'meson', 'setup', dxbuild, dxvk, '--buildtype=release',
                 '--prefix=' + str(self.prefix), '--libdir=lib', '-Denable_tests=false',
                 '-Denable_d3d9=false', '-Denable_d3d10=false', '-Ddxvk_native_wsi=sdl3',
                 '-Dc_args=-I' + str(self.prefix / 'include'),
                 '-Dcpp_args=-I' + str(self.prefix / 'include'),
                 '-Dc_link_args=-L' + str(self.prefix / 'lib'),
                 '-Dcpp_link_args=-L' + str(self.prefix / 'lib'), public_output=True)
        self.run('Compile DXVK', 'ninja', '-C', dxbuild, '-j', self.jobs, public_output=True)
        self.run('Install DXVK', 'meson', 'install', '-C', dxbuild)
        build = self.work / 'game-build'
        sdk = self.steam()
        options = ['-DTTT2_DXVK_NATIVE_ROOT=' + str(self.prefix),
                   '-DTTT2_DXVK_NATIVE_INCLUDE=' + str(dxvk / 'include/native'),
                   '-DTTT2_VKD3D_ROOT=' + str(self.prefix), '-DFFMPEG_SRC=' + str(self.ffmpeg),
                   '-DFFMPEG_BUILD=' + str(ffbuild), '-DTTT2_LINUX_PPC_OPT_LEVEL=O1',
                   '-DTTT2_LINUX_STEAM=' + ('ON' if sdk else 'OFF')]
        if sdk:
            options += ['-DTTT2_LINUX_STEAMWORKS=' + str(sdk)]
        self.cmake(self.source / 'port', build, *options)
        self.compile(build, 'tekken_recomp_linux')
        stage = self.work / 'linux-package'
        (stage / 'lib').mkdir(parents=True)
        shutil.copy2(build / 'tekken_recomp_linux', stage)
        for pattern in ('libSDL3.so*', 'libdxvk_*.so*', 'libvkd3d-shader.so*'):
            for lib in (self.prefix / 'lib').glob(pattern):
                shutil.copy2(lib, stage / 'lib' / lib.name)
        if sdk:
            shutil.copy2(sdk / 'redistributable_bin/linux64/libsteam_api.so', stage / 'lib')
        # Include the non-driver runtime dependencies, while using the host's
        # glibc, Vulkan loader/driver, display server and audio server libraries.
        for soname in ('libssl.so.3', 'libcrypto.so.3'):
            path = Path('/usr/lib/x86_64-linux-gnu') / soname
            shutil.copy2(path, stage / 'lib')
        for lib in (stage / 'lib').iterdir():
            self.run('Set relocatable library search path', 'patchelf', '--set-rpath', '$ORIGIN', lib)
        self.archive(stage, 'ttt2-linux-x86_64.tar.gz')

    def windows(self):
        ffbuild = self.configure_ffmpeg()
        # SDL's installed config uses configuration-specific DLL properties;
        # older game CMake checks the generic property before generating targets.
        shim = self.work / 'sdl-config'
        shim.mkdir()
        configs = list(self.prefix.rglob('SDL3Config.cmake'))
        if len(configs) != 1:
            raise RuntimeError('Expected one installed SDL3 CMake configuration')
        actual = configs[0].parent
        shutil.copy2(actual / 'SDL3ConfigVersion.cmake', shim)
        (shim / 'SDL3Config.cmake').write_text(
            f'include("{actual.as_posix()}/SDL3Config.cmake")\n'
            'foreach(_property IMPORTED_LOCATION IMPORTED_IMPLIB)\n'
            'get_target_property(_value SDL3::SDL3-shared ${_property}_RELEASE)\n'
            'if(_value)\nset_property(TARGET SDL3::SDL3-shared PROPERTY ${_property} "${_value}")\nendif()\n'
            'endforeach()\n')
        sdk = self.steam()
        build = self.work / 'game-build'
        options = ['-DCMAKE_BUILD_TYPE=', '-DTTT2_PPC_OPT_LEVEL=1', '-DSDL3_DIR=' + str(shim),
                   '-DFFMPEG_SRC=' + str(self.ffmpeg), '-DFFMPEG_BUILD=' + str(ffbuild)]
        if sdk:
            options += ['-DTTT2_STEAMWORKS=' + str(sdk)]
        self.cmake(self.source / 'port', build, *options)
        self.compile(build, 'tekken_recomp')
        stage = self.work / 'windows-package'
        stage.mkdir()
        shutil.copy2(build / 'tekken_recomp.exe', stage)
        for name in ('SDL3.dll', 'steam_api64.dll'):
            if (build / name).is_file():
                shutil.copy2(build / name, stage)
        crt = Path(self.env['VCToolsRedistDir']) / 'x64'
        for directory in crt.glob('Microsoft.VC*.CRT'):
            for dll in directory.glob('*.dll'):
                shutil.copy2(dll, stage)
        self.archive(stage, 'ttt2-windows-x86_64.zip')

    def archive(self, stage, name):
        if name.endswith('.zip'):
            with zipfile.ZipFile(self.output / name, 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(stage.rglob('*')):
                    if path.is_file():
                        archive.write(path, str(path.relative_to(stage)))
        else:
            with tarfile.open(self.output / name, 'w:gz') as archive:
                def clean(info):
                    info.uid = info.gid = 0
                    info.uname = info.gname = ''
                    info.mtime = 0
                    return info
                archive.add(stage, arcname='ttt2', filter=clean)

    def server(self):
        self.run('Build and test server packages', 'bash', self.source / 'server/tools/release.sh',
                 'ci-' + self.env['CI_SOURCE_SHA'][:12])
        dist = self.source / 'server/dist' / ('ci-' + self.env['CI_SOURCE_SHA'][:12])
        for pattern in ('*.zip', '*.tar.gz'):
            for package in dist.glob(pattern):
                shutil.copy2(package, self.output)

    def build(self):
        self.output.mkdir(parents=True, exist_ok=True)
        if any(self.output.iterdir()):
            raise RuntimeError('Artifact directory must start empty')
        if self.target == 'server':
            self.server()
        else:
            self.generate()
            self.dependencies()
            getattr(self, self.target)()
        files = sorted(self.output.iterdir())
        if not files or any(p.suffix not in ('.apk', '.zip', '.gz') for p in files):
            raise RuntimeError('Unexpected artifact payload')
        (self.output / 'SHA256SUMS').write_text(''.join(
            f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n' for p in files))
        print('Build complete; only packaged binaries and checksums are published.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', choices=['linux', 'windows', 'android', 'server'])
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, default=Path('private-inputs'))
    parser.add_argument('--output', type=Path, required=True)
    try:
        Build(parser.parse_args()).build()
    except (RuntimeError, OSError, KeyError, ValueError) as error:
        print(f'Build stopped: {error}', file=sys.stderr)
        sys.exit(1)
